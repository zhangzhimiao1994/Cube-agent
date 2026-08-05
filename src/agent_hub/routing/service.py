from __future__ import annotations

import asyncio
import math
import secrets
from dataclasses import dataclass
from decimal import Decimal

from agent_hub.domain.runs import TaskMode
from agent_hub.routing.classifier import RouteClassifier
from agent_hub.routing.rules import assess_rules, parse_explicit_command, validate_task_text
from agent_hub.routing.types import (
    EXECUTABLE_MODES,
    RiskLevel,
    RouteAssessment,
    RouteDecision,
    RouteSource,
)


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    confidence_threshold: float = 0.85
    max_single_cost_usd: Decimal = Decimal("0.50")
    max_total_cost_usd: Decimal = Decimal("0.75")
    classifier_timeout_seconds: float = 20
    verify_nondeterministic: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.confidence_threshold, bool)
            or not isinstance(self.confidence_threshold, int | float)
            or not math.isfinite(self.confidence_threshold)
            or not 0 <= self.confidence_threshold <= 1
        ):
            raise ValueError("confidence threshold must be finite and between zero and one")
        for name in ("max_single_cost_usd", "max_total_cost_usd"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")
        if self.max_total_cost_usd < self.max_single_cost_usd:
            raise ValueError("total cost policy cannot be below the single-assessment limit")
        if (
            isinstance(self.classifier_timeout_seconds, bool)
            or not isinstance(self.classifier_timeout_seconds, int | float)
            or not math.isfinite(self.classifier_timeout_seconds)
            or self.classifier_timeout_seconds <= 0
        ):
            raise ValueError("classifier timeout must be positive and finite")
        if type(self.verify_nondeterministic) is not bool:
            raise ValueError("verify_nondeterministic must be a boolean")


class ModeRouter:
    """Pure, side-effect-free mode selection; runtime creation belongs downstream."""

    def __init__(
        self,
        classifier: RouteClassifier,
        verifier: RouteClassifier,
        *,
        policy: RoutingPolicy | None = None,
    ) -> None:
        self._classifier = classifier
        self._verifier = verifier
        self._policy = policy or RoutingPolicy()

    async def route(self, task_text: object) -> RouteDecision:
        text = validate_task_text(task_text)
        command = parse_explicit_command(text)
        if command.invalid:
            return self._waiting("invalid_explicit_command")
        if command.mode is not None and command.mode is not TaskMode.AUTO:
            rule = assess_rules(command.task_text)
            risk = rule.risk
            return self._ready(
                command.mode,
                (
                    self._local_assessment(
                        command.mode,
                        "explicit_user_command",
                        RouteSource.EXPLICIT,
                        risk=risk,
                    ),
                ),
                risk=risk,
                requires_approval=rule.requires_approval,
            )
        effective_text = command.task_text if command.mode is TaskMode.AUTO else text
        rules = assess_rules(effective_text)
        if rules.reason is not None and rules.mode is None:
            return self._waiting(
                rules.reason,
                risk=rules.risk,
                requires_approval=rules.requires_approval,
            )
        if rules.mode is not None:
            return self._ready(
                rules.mode,
                (
                    self._local_assessment(
                        rules.mode,
                        rules.reason or "deterministic_rule",
                        RouteSource.RULE,
                        risk=rules.risk,
                    ),
                ),
                risk=rules.risk,
                requires_approval=rules.requires_approval,
            )
        assessments = await self._classify(effective_text)
        if assessments is None:
            return self._waiting("classification_unavailable")
        highest_risk = max((item.risk for item in assessments), key=_risk_rank)
        requires_approval = highest_risk is RiskLevel.HIGH
        if (
            any(item.confidence < self._policy.confidence_threshold for item in assessments)
            or any(item.mode is not assessments[0].mode for item in assessments[1:])
            or highest_risk is RiskLevel.HIGH
            or any(
                item.estimated_cost_usd > self._policy.max_single_cost_usd
                for item in assessments
            )
            or sum((item.estimated_cost_usd for item in assessments), Decimal(0))
            > self._policy.max_total_cost_usd
        ):
            return self._waiting(
                "routing_requires_user_choice",
                assessments,
                risk=highest_risk,
                requires_approval=requires_approval,
            )
        return self._ready(
            assessments[0].mode,
            assessments,
            risk=highest_risk,
            requires_approval=requires_approval,
        )

    async def _classify(self, task_text: str) -> tuple[RouteAssessment, ...] | None:
        classifiers = (
            (self._classifier, self._verifier)
            if self._policy.verify_nondeterministic
            else (self._classifier,)
        )
        tasks = tuple(asyncio.create_task(item.classify(task_text)) for item in classifiers)
        try:
            try:
                completed: dict[asyncio.Task[RouteAssessment], RouteAssessment] = {}
                pending = set(tasks)
                async with asyncio.timeout(self._policy.classifier_timeout_seconds):
                    while pending:
                        done, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            try:
                                completed[task] = task.result()
                            except asyncio.CancelledError:
                                raise
                            except Exception:  # noqa: BLE001 - classifier boundary is redacted
                                return None
            except TimeoutError:
                return None
            assessments = tuple(completed[task] for task in tasks)
            expected_sources = (
                (RouteSource.CLASSIFIER, RouteSource.VERIFIER)
                if self._policy.verify_nondeterministic
                else (RouteSource.CLASSIFIER,)
            )
            if tuple(item.source for item in assessments) != expected_sources:
                return None
            return assessments
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def confirm_mode(
        self,
        decision: RouteDecision,
        mode: TaskMode,
        *,
        decision_token: str | None,
        version: int,
    ) -> RouteDecision:
        if (
            decision.status != "waiting_user_mode"
            or decision_token is None
            or not secrets.compare_digest(decision_token, decision.decision_token or "")
            or version != decision.version
        ):
            raise ValueError("stale routing decision")
        if mode not in EXECUTABLE_MODES:
            raise ValueError("confirmed mode must be executable")
        user = self._local_assessment(mode, "explicit_user_confirmation", RouteSource.USER, risk=decision.risk)
        return self._ready(
            mode,
            (*decision.assessments, user),
            risk=decision.risk,
            requires_approval=decision.requires_approval,
            version=decision.version + 1,
        )

    @staticmethod
    def _local_assessment(
        mode: TaskMode,
        reason: str,
        source: RouteSource,
        *,
        risk: RiskLevel,
    ) -> RouteAssessment:
        return RouteAssessment(
            mode=mode,
            confidence=1.0,
            reason=reason,
            roles=(),
            estimated_seconds=0,
            estimated_cost_usd=Decimal(0),
            risk=risk,
            source=source,
            logical_model="local",
            deployment_id="local",
            provider_id="local",
        )

    @staticmethod
    def _ready(
        mode: TaskMode,
        assessments: tuple[RouteAssessment, ...],
        *,
        risk: RiskLevel,
        requires_approval: bool,
        version: int = 1,
    ) -> RouteDecision:
        return RouteDecision(
            mode=mode,
            needs_user_choice=False,
            status="ready",
            assessments=assessments,
            clarification_reason=None,
            options=(),
            decision_token=None,
            version=version,
            risk=risk,
            requires_approval=requires_approval,
            permissions_still_apply=True,
        )

    @staticmethod
    def _waiting(
        reason: str,
        assessments: tuple[RouteAssessment, ...] = (),
        *,
        risk: RiskLevel = RiskLevel.LOW,
        requires_approval: bool = False,
    ) -> RouteDecision:
        return RouteDecision(
            mode=None,
            needs_user_choice=True,
            status="waiting_user_mode",
            assessments=assessments,
            clarification_reason=reason,
            options=EXECUTABLE_MODES,
            decision_token=secrets.token_urlsafe(32),
            version=1,
            risk=risk,
            requires_approval=requires_approval,
            permissions_still_apply=True,
        )


def _risk_rank(value: RiskLevel) -> int:
    return {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}[value]
