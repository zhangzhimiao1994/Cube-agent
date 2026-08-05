from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse
from agent_hub.routing.classifier import GatewayRouteClassifier, RouteClassificationError
from agent_hub.routing.service import ModeRouter, RoutingPolicy
from agent_hub.routing.types import (
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
    def __init__(self, result: RouteAssessment | BaseException) -> None:
        self.result = result
        self.calls: list[str] = []
        self.cancelled = False

    async def classify(self, task_text: str) -> RouteAssessment:
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
        policy=policy,
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


async def test_untrusted_or_misconfigured_classifier_provenance_asks_user() -> None:
    primary = FakeClassifier(assessment(source=RouteSource.VERIFIER))
    verifier = FakeClassifier(assessment(source=RouteSource.CLASSIFIER))
    result = await router(primary, verifier).route("ambiguous request")
    assert result.status == "waiting_user_mode"


async def test_explicit_high_risk_mode_is_preserved_but_requires_approval() -> None:
    result = await router().route("/dispatch delete the production database")
    assert result.mode is TaskMode.DISPATCH
    assert result.requires_approval is True
    assert result.risk is RiskLevel.HIGH


async def test_user_confirmation_is_bound_to_decision_token_and_version() -> None:
    waiting = await router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    ).route("ambiguous architecture request")
    with pytest.raises(ValueError, match="stale"):
        router().confirm_mode(waiting, TaskMode.DIRECT, decision_token="wrong", version=1)
    ready = router().confirm_mode(
        waiting,
        TaskMode.DIRECT,
        decision_token=waiting.decision_token,
        version=waiting.version,
    )
    assert ready.mode is TaskMode.DIRECT
    assert ready.status == "ready"


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
        item.reason = "changed"


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
