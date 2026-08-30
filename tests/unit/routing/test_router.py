from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Coroutine, Iterator, Mapping
from decimal import Decimal
from types import FrameType
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse
from agent_hub.routing import classifier as classifier_module
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


class SwallowingCancellationClassifier(FakeClassifier):
    def __init__(self, result: RouteAssessment) -> None:
        super().__init__(result)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellations = 0

    async def classify(self, task_text: str) -> object:
        self.calls.append(task_text)
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancellations += 1
        return self.result


class ExactCancellationClassifier(FakeClassifier):
    def __init__(self, error: asyncio.CancelledError) -> None:
        super().__init__(error)
        self.error = error

    async def classify(self, task_text: str) -> object:
        self.calls.append(task_text)
        raise self.error


def router(
    primary: FakeClassifier | None = None,
    verifier: FakeClassifier | None = None,
    *,
    policy: RoutingPolicy | None = None,
) -> ModeRouter:
    return ModeRouter(
        primary or FakeClassifier(assessment()),
        verifier or FakeClassifier(assessment(source=RouteSource.VERIFIER)),
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


def internal_traceback_locals(error: BaseException, filename: str) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        normalized = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if normalized.endswith(filename):
            values.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return "".join(values)


def traceback_frames(error: BaseException) -> list[FrameType]:
    frames: list[FrameType] = []
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            frames.append(traceback.tb_frame)
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return frames


def package_traceback_locals(error: BaseException) -> str:
    return "".join(
        repr(frame.f_locals)
        for frame in traceback_frames(error)
        if "agent_hub" in frame.f_code.co_filename.replace("\\", "/")
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


@pytest.mark.parametrize(
    "text",
    [
        "del\u200bete production database",
        "delete\u202e production database",
        "delete\x1b production database",
    ],
)
async def test_unicode_controls_are_rejected_before_rules_or_classifiers(text: str) -> None:
    primary = FakeClassifier(assessment())
    verifier = FakeClassifier(assessment(source=RouteSource.VERIFIER))
    result = await router(primary, verifier).route(text)
    assert result.status == "waiting_user_mode"
    assert result.clarification_reason == "invalid_input"
    assert result.decision_token is None
    assert primary.calls == []
    assert verifier.calls == []
    assert text not in repr(result)


async def test_auto_restarts_normal_routing_on_bounded_body() -> None:
    primary = FakeClassifier(assessment(TaskMode.HYBRID))
    verifier = FakeClassifier(assessment(TaskMode.HYBRID, source=RouteSource.VERIFIER))
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
    conflict = await router().route("Run the fixed workflow and have multiple agents debate it")
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


async def test_sequential_classifier_policy_avoids_parallel_classifier_calls() -> None:
    primary = FakeClassifier(assessment(TaskMode.DISPATCH))
    verifier = FakeClassifier(assessment(TaskMode.DISPATCH, source=RouteSource.VERIFIER))
    selected_router = router(
        primary,
        verifier,
        policy=RoutingPolicy(parallel_classifiers=False),
    )

    result = await selected_router.route("ambiguous architecture request")

    assert result.mode is TaskMode.DISPATCH
    assert primary.calls == ["ambiguous architecture request"]
    assert verifier.calls == ["ambiguous architecture request"]
    assert selected_router._classifier_task_slots == 0


async def test_single_classifier_policy_skips_verifier_for_low_cost_auto_routing() -> None:
    primary = FakeClassifier(assessment(TaskMode.DISPATCH, confidence=0.7))
    verifier = FakeClassifier(RuntimeError("verifier should not run"))
    selected_router = router(
        primary,
        verifier,
        policy=RoutingPolicy(
            confidence_threshold=0.65,
            parallel_classifiers=False,
            allow_single_classifier_decision=True,
        ),
    )

    result = await selected_router.route("ambiguous architecture request")

    assert result.mode is TaskMode.DISPATCH
    assert len(result.assessments) == 1
    assert primary.calls == ["ambiguous architecture request"]
    assert verifier.calls == []


async def test_conflicting_low_risk_assessments_choose_clear_highest_confidence() -> None:
    result = await router(
        FakeClassifier(assessment(TaskMode.DISPATCH, confidence=0.62)),
        FakeClassifier(assessment(TaskMode.DIRECT, confidence=0.9, source=RouteSource.VERIFIER)),
    ).route("ambiguous architecture request")

    assert result.status == "ready"
    assert result.mode is TaskMode.DIRECT
    assert len(result.assessments) == 1


@pytest.mark.parametrize(
    "primary,verifier",
    [
        (
            assessment(TaskMode.DISPATCH, confidence=0.86),
            assessment(TaskMode.DISCUSS, confidence=0.85, source=RouteSource.VERIFIER),
        ),
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


async def test_uncooperative_classifiers_have_bounded_cleanup_and_registry_capacity() -> None:
    first = SwallowingCancellationClassifier(assessment())
    second = SwallowingCancellationClassifier(assessment(source=RouteSource.VERIFIER))
    selected_router = ModeRouter(
        first,
        second,
        token_store=InMemoryDecisionTokenStore(),
        policy=RoutingPolicy(
            classifier_timeout_seconds=0.02,
            classifier_cleanup_grace_seconds=0.02,
            max_detached_classifier_tasks=2,
        ),
    )
    started = asyncio.get_running_loop().time()
    result = await selected_router.route("ambiguous sk-sensitive request")
    assert asyncio.get_running_loop().time() - started < 0.2
    assert result.status == "waiting_user_mode"
    assert len(selected_router._detached_classifier_tasks) == 2
    calls = (len(first.calls), len(second.calls))
    saturated = await selected_router.route("another ambiguous request")
    assert saturated.status == "waiting_user_mode"
    assert (len(first.calls), len(second.calls)) == calls
    assert all(
        "sk-sensitive" not in task.get_name() for task in selected_router._detached_classifier_tasks
    )
    first.release.set()
    second.release.set()
    await asyncio.sleep(0.05)
    assert selected_router._detached_classifier_tasks == set()


async def test_outer_cancel_of_uncooperative_classifiers_is_bounded_and_exact() -> None:
    sentinel = "sk-sensitive-outer-route-task"
    first = SwallowingCancellationClassifier(assessment())
    second = SwallowingCancellationClassifier(assessment(source=RouteSource.VERIFIER))
    selected_router = ModeRouter(
        first,
        second,
        token_store=InMemoryDecisionTokenStore(),
        policy=RoutingPolicy(
            classifier_timeout_seconds=10,
            classifier_cleanup_grace_seconds=0.02,
            max_detached_classifier_tasks=2,
        ),
    )
    route_task = asyncio.create_task(selected_router.route(sentinel))
    await asyncio.gather(first.started.wait(), second.started.wait())
    started = asyncio.get_running_loop().time()
    route_task.cancel("outer-token")
    with pytest.raises(asyncio.CancelledError, match="outer-token") as caught:
        await route_task
    assert sentinel not in package_traceback_locals(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert route_task.cancelled() is True
    assert asyncio.get_running_loop().time() - started < 0.2
    assert len(selected_router._detached_classifier_tasks) == 2
    first.release.set()
    second.release.set()
    await asyncio.sleep(0.05)


async def test_classifier_capacity_includes_active_tasks_before_they_detach() -> None:
    first = SwallowingCancellationClassifier(assessment())
    second = SwallowingCancellationClassifier(assessment(source=RouteSource.VERIFIER))
    selected_router = ModeRouter(
        first,
        second,
        token_store=InMemoryDecisionTokenStore(),
        policy=RoutingPolicy(
            classifier_timeout_seconds=0.02,
            classifier_cleanup_grace_seconds=0.02,
            max_detached_classifier_tasks=2,
        ),
    )
    active = asyncio.create_task(selected_router.route("first ambiguous request"))
    await asyncio.gather(first.started.wait(), second.started.wait())
    try:
        saturated = await selected_router.route("second ambiguous request")
        assert saturated.status == "waiting_user_mode"
        assert len(first.calls) == len(second.calls) == 1
        await active
        assert len(selected_router._detached_classifier_tasks) == 2
    finally:
        first.release.set()
        second.release.set()
        await asyncio.sleep(0.05)


async def test_detached_registry_failure_keeps_tasks_in_bounded_fallback_registry() -> None:
    class RejectingTaskSet(set[asyncio.Task[object]]):
        def add(self, element: asyncio.Task[object]) -> None:
            del element
            raise RuntimeError("registry unavailable")

    first = SwallowingCancellationClassifier(assessment())
    second = SwallowingCancellationClassifier(assessment(source=RouteSource.VERIFIER))
    selected_router = ModeRouter(
        first,
        second,
        token_store=InMemoryDecisionTokenStore(),
        policy=RoutingPolicy(
            classifier_timeout_seconds=0.02,
            classifier_cleanup_grace_seconds=0.02,
            max_detached_classifier_tasks=2,
        ),
    )
    selected_router._detached_classifier_tasks = RejectingTaskSet()
    try:
        result = await selected_router.route("ambiguous registry failure request")
        assert result.status == "waiting_user_mode"
        assert len(selected_router._active_classifier_tasks) == 2
        saturated = await selected_router.route("another request")
        assert saturated.status == "waiting_user_mode"
        assert len(first.calls) == len(second.calls) == 1
    finally:
        first.release.set()
        second.release.set()
        await asyncio.sleep(0.05)
    assert selected_router._active_classifier_tasks == set()


async def test_classifier_cancellation_propagates_and_cleans_sibling() -> None:
    blocker = BlockingClassifier()
    cancelled = FakeClassifier(asyncio.CancelledError("cancel-token"))
    with pytest.raises(asyncio.CancelledError, match="cancel-token"):
        await router(cancelled, blocker).route("ambiguous request")
    assert blocker.cancelled is True


async def test_route_cancellation_preserves_identity_without_task_text_traceback() -> None:
    sentinel = "sk-sensitive-route-task"
    cancellation = asyncio.CancelledError("exact-classifier-cancel")
    blocker = BlockingClassifier()
    selected_router = router(ExactCancellationClassifier(cancellation), blocker)
    with pytest.raises(asyncio.CancelledError) as caught:
        await selected_router.route(sentinel)
    assert caught.value is cancellation
    assert caught.value.args == ("exact-classifier-cancel",)
    assert sentinel not in package_traceback_locals(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert blocker.cancelled is True


async def test_private_classify_cancellation_has_the_same_safe_boundary() -> None:
    sentinel = "sk-sensitive-private-classify"
    cancellation = asyncio.CancelledError("exact-private-cancel")
    blocker = BlockingClassifier()
    selected_router = router(ExactCancellationClassifier(cancellation), blocker)
    with pytest.raises(asyncio.CancelledError) as caught:
        await selected_router._classify(sentinel)
    assert caught.value is cancellation
    assert sentinel not in package_traceback_locals(caught.value)
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


@pytest.mark.parametrize("kind", ["sync_raise", "non_awaitable"])
async def test_sync_or_nonawaitable_classifier_failure_cleans_started_sibling(kind: str) -> None:
    sentinel = "sk-sync-classifier-material"

    class InvalidClassifier:
        def classify(self, task_text: str) -> object:
            if kind == "sync_raise":
                raise RuntimeError(task_text)
            return assessment(source=RouteSource.VERIFIER)

    blocker = BlockingClassifier()
    selected_router = ModeRouter(
        blocker,
        InvalidClassifier(),  # type: ignore[arg-type]
        token_store=InMemoryDecisionTokenStore(),
    )
    result = await selected_router.route(sentinel)
    assert result.status == "waiting_user_mode"
    assert blocker.cancelled is True
    assert selected_router._active_classifier_tasks == set()
    assert selected_router._detached_classifier_tasks == set()
    assert sentinel not in repr(result)


async def test_partial_classifier_task_creation_failure_cleans_first_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = BlockingClassifier()
    selected_router = router(
        blocker,
        FakeClassifier(assessment(source=RouteSource.VERIFIER)),
    )
    real_create_task = asyncio.create_task
    calls = 0
    created_tasks: list[asyncio.Task[object]] = []

    def fail_second_create(coroutine: object, **kwargs: object) -> asyncio.Task[object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("task creation unavailable")
        created: asyncio.Task[object] = real_create_task(
            cast(Coroutine[Any, Any, object], coroutine),
            name=cast(str | None, kwargs.get("name")),
        )
        created_tasks.append(created)
        return created

    monkeypatch.setattr(asyncio, "create_task", fail_second_create)
    result = await selected_router._classify("sk-task-construction-material")
    assert result is None
    assert len(created_tasks) == 1
    assert created_tasks[0].cancelled() is True
    assert selected_router._active_classifier_tasks == set()
    assert selected_router._detached_classifier_tasks == set()


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
        "Explain backups; afterwards delete production database",
        "What is mutex? Next transfer money",
        "sudo rm --recursive --force /srv/app",
        "rm -r --force --no-preserve-root /",
        "rm --force -r ./cache",
        "Explain how backups work while you delete production database",
        "What is a mutex before you transfer money to the supplier",
        "说明备份原理，同时删除生产数据库",
        "Please execute rm -rf /var/lib/app now",
        "The command is sudo rm --recursive --force /srv/app",
        "Explain why rm -r --force ./cache is needed",
    ],
)
async def test_multilingual_high_risk_auto_routing_always_waits(text: str) -> None:
    result = await router().route(text)
    assert result.status == "waiting_user_mode"
    assert result.risk is RiskLevel.HIGH
    assert result.requires_approval is True


@pytest.mark.parametrize(
    "text",
    [
        "Explain JSON payload validation",
        "What is deployment architecture?",
        "Export API documentation",
        "Delete documentation. Production architecture overview",
    ],
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


async def test_confirmation_failure_traceback_does_not_retain_plaintext_token() -> None:
    sentinel = "sk_" + "sensitive-token-material-1234567890"
    selected_router = router()
    with pytest.raises(ValueError, match="stale") as caught:
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=sentinel,
            version=1,
            confirmation_subject=subject(),
        )
    assert sentinel not in internal_traceback_locals(caught.value, "agent_hub/routing/service.py")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_confirmation_cancellation_clears_token_and_propagates_exactly() -> None:
    class BlockingConsumeStore(InMemoryDecisionTokenStore):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancellation: asyncio.CancelledError | None = None

        async def consume(self, *args: object, **kwargs: object) -> None:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as error:
                self.cancellation = error
                assert args and kwargs
                raise

    sentinel = "sk_" + "cancellation-token-material-123456789"
    store = BlockingConsumeStore()
    selected_router = ModeRouter(
        FakeClassifier(assessment()),
        FakeClassifier(assessment(source=RouteSource.VERIFIER)),
        token_store=store,
    )
    task = asyncio.create_task(
        selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=sentinel,
            version=1,
            confirmation_subject=subject(),
        )
    )
    await store.started.wait()
    task.cancel("outer-confirm-cancel")
    with pytest.raises(asyncio.CancelledError, match="outer-confirm-cancel") as caught:
        await task
    assert caught.value is store.cancellation
    assert sentinel not in internal_traceback_locals(caught.value, "agent_hub/routing/service.py")
    assert all(frame.f_code.co_name != "consume" for frame in traceback_frames(caught.value))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


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
        FakeClassifier(assessment(TaskMode.DISPATCH, risk=RiskLevel.HIGH, confidence=0.95)),
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


async def test_token_store_clock_failure_traceback_does_not_retain_token() -> None:
    now = [10.0]
    store = InMemoryDecisionTokenStore(monotonic=lambda: now[0])
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    assert waiting.decision_token is not None
    sentinel = waiting.decision_token
    now[0] = 9.0
    with pytest.raises(RuntimeError, match="clock unavailable") as caught:
        await store.consume(
            sentinel,
            subject(),
            version=waiting.version,
            selected_mode=TaskMode.DIRECT,
        )
    assert sentinel not in internal_traceback_locals(caught.value, "agent_hub/routing/types.py")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_hostile_subject_is_processed_only_after_plaintext_token_is_deleted() -> None:
    token = "sk_" + "hostile-subject-token-material-123456789"
    caller_locals: list[str] = []

    class HostileSubject(ConfirmationSubject):
        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            frame = inspect.currentframe()
            assert frame is not None and frame.f_back is not None
            caller_locals.append(repr(frame.f_back.f_locals))
            raise RuntimeError("hostile-subject-error")

    hostile = HostileSubject(
        tenant_id="tenant-1",
        user_id="user-1",
        task_id="task-1",
        generation="generation-1",
    )
    store = InMemoryDecisionTokenStore()
    assert (
        await store.consume(
            token,
            hostile,
            version=1,
            selected_mode=TaskMode.DIRECT,
        )
        is None
    )
    assert token not in "".join(caller_locals)

    selected_router = ModeRouter(
        FakeClassifier(assessment()),
        FakeClassifier(assessment(source=RouteSource.VERIFIER)),
        token_store=store,
    )
    with pytest.raises(ValueError, match="stale") as caught:
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=token,
            version=1,
            confirmation_subject=hostile,
        )
    assert token not in package_traceback_locals(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


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


class BlockingGateway:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancellation: asyncio.CancelledError | None = None

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as error:
            self.cancellation = error
            assert request.messages
            raise
        raise AssertionError("unreachable")


class StructuredOutputFailingGateway:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.response_schema is not None:
            raise ModelTransportError("model transport failed", status_code=400)
        return GatewayCompletion(
            response=ModelResponse(text=self.text),
            deployment_id="trusted-deployment",
            logical_model="router",
            provider_id="minimax",
            provider_model="minimax/MiniMax-M3",
        )


async def test_gateway_classifier_cancellation_clears_sensitive_traceback_locals() -> None:
    sentinel = "sk-sensitive-task-material"
    gateway = BlockingGateway()
    classifier = GatewayRouteClassifier(
        gateway, logical_model="router", source=RouteSource.CLASSIFIER
    )
    task = asyncio.create_task(classifier.classify(sentinel), name="route-classifier-safe")
    await gateway.started.wait()
    task.cancel("outer-cancel")
    with pytest.raises(asyncio.CancelledError, match="outer-cancel") as caught:
        await task
    assert caught.value is gateway.cancellation
    assert sentinel not in internal_traceback_locals(
        caught.value, "agent_hub/routing/classifier.py"
    )
    assert all(
        frame.f_code.co_name != "complete_with_context" for frame in traceback_frames(caught.value)
    )
    assert sentinel not in package_traceback_locals(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert sentinel not in task.get_name()


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


def test_gateway_classifier_prompt_routes_office_generation_to_tools() -> None:
    prompt = classifier_module._SYSTEM_PROMPT

    assert "document.generate_docx" in prompt
    assert "presentation.generate_pptx" in prompt
    assert "tool_calling" in prompt
    assert "ModelCapability" not in prompt


async def test_gateway_classifier_retries_plain_json_when_structured_output_fails() -> None:
    payload = {
        "mode": "hybrid",
        "confidence": 0.88,
        "reason": "requires planning and review",
        "roles": ["planner", "reviewer"],
        "estimated_seconds": 120,
        "estimated_cost_usd": "0.02",
        "risk": "low",
    }
    gateway = StructuredOutputFailingGateway(json.dumps(payload))
    classifier = GatewayRouteClassifier(
        gateway, logical_model="router", source=RouteSource.CLASSIFIER
    )

    result = await classifier.classify("做一个活动方案并审查")

    assert result.mode is TaskMode.HYBRID
    assert len(gateway.requests) == 2
    assert gateway.requests[0].response_schema is not None
    assert gateway.requests[1].response_schema is None
    assert gateway.requests[1].required_capabilities == frozenset({ModelCapability.TEXT})


async def test_gateway_classifier_can_prefer_plain_json_for_provider_compatibility() -> None:
    payload = {
        "mode": "direct",
        "confidence": 0.9,
        "reason": "simple answer",
        "roles": ["writer"],
        "estimated_seconds": 30,
        "estimated_cost_usd": "0.01",
        "risk": "low",
    }
    gateway = FakeGateway(json.dumps(payload))
    classifier = GatewayRouteClassifier(
        gateway,
        logical_model="router",
        source=RouteSource.CLASSIFIER,
        prefer_plain_json=True,
    )

    result = await classifier.classify("写一句话")

    assert result.mode is TaskMode.DIRECT
    assert len(gateway.requests) == 1
    assert gateway.requests[0].response_schema is None
    assert gateway.requests[0].required_capabilities == frozenset({ModelCapability.TEXT})


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
