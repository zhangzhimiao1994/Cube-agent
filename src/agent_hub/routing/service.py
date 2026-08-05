from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError

from agent_hub.domain.runs import TaskMode
from agent_hub.routing.classifier import RouteClassifier
from agent_hub.routing.rules import (
    RiskRulePolicy,
    assess_rules,
    normalize_task_text,
    parse_explicit_command,
    validate_task_text,
)
from agent_hub.routing.types import (
    EXECUTABLE_MODES,
    ConfirmationSnapshot,
    ConfirmationSubject,
    ConsumedDecisionToken,
    DecisionTokenStore,
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
    classifier_cleanup_grace_seconds: float = 0.1
    max_detached_classifier_tasks: int = 32
    confirmation_ttl_seconds: float = 300

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
        if (
            isinstance(self.classifier_cleanup_grace_seconds, bool)
            or not isinstance(self.classifier_cleanup_grace_seconds, int | float)
            or not math.isfinite(self.classifier_cleanup_grace_seconds)
            or not 0 < self.classifier_cleanup_grace_seconds <= 1
        ):
            raise ValueError("classifier cleanup grace must be finite and at most one second")
        if (
            type(self.max_detached_classifier_tasks) is not int
            or not 2 <= self.max_detached_classifier_tasks <= 256
        ):
            raise ValueError("detached classifier task capacity must be between 2 and 256")
        if (
            isinstance(self.confirmation_ttl_seconds, bool)
            or not isinstance(self.confirmation_ttl_seconds, int | float)
            or not math.isfinite(self.confirmation_ttl_seconds)
            or self.confirmation_ttl_seconds <= 0
        ):
            raise ValueError("confirmation TTL must be positive and finite")


class ModeRouter:
    """Pure, side-effect-free mode selection; runtime creation belongs downstream."""

    def __init__(
        self,
        classifier: RouteClassifier,
        verifier: RouteClassifier,
        *,
        token_store: DecisionTokenStore,
        policy: RoutingPolicy | None = None,
        risk_policy: RiskRulePolicy | None = None,
    ) -> None:
        if classifier is verifier:
            raise ValueError("classifier and verifier must be independent instances")
        self._classifier = classifier
        self._verifier = verifier
        self._token_store = token_store
        self._policy = policy or RoutingPolicy()
        self._risk_policy = risk_policy or RiskRulePolicy()
        self._active_classifier_tasks: set[asyncio.Task[object]] = set()
        self._detached_classifier_tasks: set[asyncio.Task[object]] = set()

    async def route(
        self,
        task_text: object,
        *,
        confirmation_subject: ConfirmationSubject | None = None,
    ) -> RouteDecision:
        try:
            text = validate_task_text(task_text)
        except (TypeError, ValueError):
            return await self._waiting("invalid_input")
        command = parse_explicit_command(text)
        if command.invalid:
            return await self._waiting(
                "invalid_explicit_command", confirmation_subject=confirmation_subject
            )
        if command.mode is not None and command.mode is not TaskMode.AUTO:
            normalized_body = normalize_task_text(command.task_text)
            rule = assess_rules(normalized_body, risk_policy=self._risk_policy)
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
        effective_text = normalize_task_text(
            command.task_text if command.mode is TaskMode.AUTO else text
        )
        rules = assess_rules(effective_text, risk_policy=self._risk_policy)
        if rules.reason is not None and rules.mode is None:
            return await self._waiting(
                rules.reason,
                risk=rules.risk,
                requires_approval=rules.requires_approval,
                confirmation_subject=confirmation_subject,
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
            return await self._waiting(
                "classification_unavailable", confirmation_subject=confirmation_subject
            )
        highest_risk = max((item.risk for item in assessments), key=_risk_rank)
        requires_approval = highest_risk is RiskLevel.HIGH
        if (
            any(item.confidence < self._policy.confidence_threshold for item in assessments)
            or any(item.mode is not assessments[0].mode for item in assessments[1:])
            or highest_risk is RiskLevel.HIGH
            or any(
                item.estimated_cost_usd > self._policy.max_single_cost_usd for item in assessments
            )
            or sum((item.estimated_cost_usd for item in assessments), Decimal(0))
            > self._policy.max_total_cost_usd
        ):
            return await self._waiting(
                "routing_requires_user_choice",
                assessments,
                risk=highest_risk,
                requires_approval=requires_approval,
                confirmation_subject=confirmation_subject,
            )
        return self._ready(
            assessments[0].mode,
            assessments,
            risk=highest_risk,
            requires_approval=requires_approval,
        )

    async def _classify(self, task_text: str) -> tuple[RouteAssessment, ...] | None:
        if (
            len(self._active_classifier_tasks) + len(self._detached_classifier_tasks) + 2
            > self._policy.max_detached_classifier_tasks
        ):
            return None
        classifiers = (self._classifier, self._verifier)
        tasks = tuple(
            asyncio.create_task(
                item.classify(task_text),
                name=f"route-classifier-{index}",
            )
            for index, item in enumerate(classifiers)
        )
        self._active_classifier_tasks.update(tasks)
        result: tuple[RouteAssessment, ...] | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            try:
                completed: dict[asyncio.Task[object], object] = {}
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
                                pending = set()
                                break
            except TimeoutError:
                completed = {}
            if len(completed) == len(tasks):
                validated = tuple(
                    self._validate_assessment(completed[task], source)
                    for task, source in zip(
                        tasks,
                        (RouteSource.CLASSIFIER, RouteSource.VERIFIER),
                        strict=True,
                    )
                )
                if not any(item is None for item in validated):
                    result = tuple(item for item in validated if item is not None)
        except asyncio.CancelledError as error:
            cancellation = error
        finally:
            try:
                await self._bounded_classifier_cleanup(tasks)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
        if cancellation is not None:
            raise cancellation
        return result

    async def _bounded_classifier_cleanup(self, tasks: tuple[asyncio.Task[object], ...]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        interrupted: asyncio.CancelledError | None = None
        done: set[asyncio.Task[object]] = {task for task in tasks if task.done()}
        pending: set[asyncio.Task[object]] = set(tasks) - done
        if pending:
            try:
                newly_done, pending = await asyncio.wait(
                    pending,
                    timeout=self._policy.classifier_cleanup_grace_seconds,
                )
                done.update(newly_done)
            except asyncio.CancelledError as error:
                interrupted = error
                done = {task for task in tasks if task.done()}
                pending = set(tasks) - done
        for task in done:
            self._active_classifier_tasks.discard(task)
            self._consume_classifier_task(task)
        for task in pending:
            task.cancel()
            self._active_classifier_tasks.discard(task)
            self._register_detached_classifier_task(task)
        if interrupted is not None:
            raise interrupted

    def _register_detached_classifier_task(self, task: asyncio.Task[object]) -> None:
        if task.done():
            self._consume_classifier_task(task)
            return
        self._detached_classifier_tasks.add(task)
        task.add_done_callback(self._detached_classifier_done)

    def _detached_classifier_done(self, task: asyncio.Task[object]) -> None:
        self._detached_classifier_tasks.discard(task)
        self._consume_classifier_task(task)

    @staticmethod
    def _consume_classifier_task(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:  # noqa: BLE001 - retrieve every terminal task failure
            return

    @staticmethod
    def _validate_assessment(value: object, source: RouteSource) -> RouteAssessment | None:
        try:
            return ModeRouter._validate_assessment_unchecked(value, source)
        except Exception as error:  # noqa: BLE001 - untrusted classifier object boundary
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            return None

    @staticmethod
    def _validate_assessment_unchecked(
        value: object, source: RouteSource
    ) -> RouteAssessment | None:
        expected_keys = {
            "mode",
            "confidence",
            "reason",
            "roles",
            "estimated_seconds",
            "estimated_cost_usd",
            "risk",
            "source",
            "logical_model",
            "deployment_id",
            "provider_id",
        }
        if not isinstance(value, RouteAssessment):
            return None
        raw = value.model_dump(round_trip=True)
        if set(raw) != expected_keys:
            return None
        roles = raw.get("roles")
        if not isinstance(roles, tuple | list) or len(roles) > 8:
            return None
        normalized = {key: raw[key] for key in expected_keys if key not in {"reason", "source"}}
        normalized["reason"] = (
            "classifier_recommendation"
            if source is RouteSource.CLASSIFIER
            else "verifier_recommendation"
        )
        normalized["source"] = source
        try:
            return RouteAssessment.model_validate(normalized, strict=True)
        except (TypeError, ValueError, ValidationError) as error:
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            return None

    async def confirm_mode(
        self,
        mode: TaskMode,
        *,
        decision_token: str | None,
        version: int,
        confirmation_subject: ConfirmationSubject,
    ) -> RouteDecision:
        plaintext_token = decision_token
        decision_token = None
        del decision_token
        try:
            consumed = await self._consume_confirmation(
                mode,
                plaintext_token=plaintext_token,
                version=version,
                confirmation_subject=confirmation_subject,
            )
        finally:
            plaintext_token = None
            del plaintext_token
        if consumed is None:
            raise ValueError("stale routing decision") from None
        snapshot = consumed.snapshot
        user = self._local_assessment(
            mode, "explicit_user_confirmation", RouteSource.USER, risk=snapshot.risk
        )
        return self._ready(
            mode,
            (*snapshot.assessments, user),
            risk=snapshot.risk,
            requires_approval=snapshot.requires_approval,
            version=consumed.version + 1,
        )

    async def _consume_confirmation(
        self,
        mode: TaskMode,
        *,
        plaintext_token: str | None,
        version: int,
        confirmation_subject: ConfirmationSubject,
    ) -> ConsumedDecisionToken | None:
        try:
            try:
                confirmation_subject = ConfirmationSubject.model_validate(
                    confirmation_subject.model_dump(round_trip=True), strict=True
                )
            except (AttributeError, TypeError, ValueError, ValidationError) as error:
                error.__traceback__ = None
                error.__context__ = None
                error.__cause__ = None
                return None
            if plaintext_token is None or type(version) is not int or version <= 0:
                return None
            if not isinstance(mode, TaskMode) or mode not in EXECUTABLE_MODES:
                raise ValueError("confirmed mode must be executable") from None
            try:
                return await self._token_store.consume(
                    plaintext_token,
                    confirmation_subject,
                    version=version,
                    selected_mode=mode,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - stable confirmation boundary
                error.__traceback__ = None
                error.__context__ = None
                error.__cause__ = None
                return None
        finally:
            plaintext_token = None
            del plaintext_token

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

    async def _waiting(
        self,
        reason: str,
        assessments: tuple[RouteAssessment, ...] = (),
        *,
        risk: RiskLevel = RiskLevel.LOW,
        requires_approval: bool = False,
        confirmation_subject: ConfirmationSubject | None = None,
    ) -> RouteDecision:
        if not assessments and risk is RiskLevel.HIGH:
            assessments = (
                self._local_assessment(
                    TaskMode.DISPATCH,
                    "high_risk_policy",
                    RouteSource.RULE,
                    risk=RiskLevel.HIGH,
                ),
            )
        token: str | None = None
        version = 1
        if confirmation_subject is not None:
            try:
                snapshot = ConfirmationSnapshot(
                    assessments=assessments,
                    clarification_reason=reason,
                    options=EXECUTABLE_MODES,
                    risk=risk,
                    requires_approval=requires_approval,
                )
                issued = await self._token_store.issue(
                    confirmation_subject,
                    snapshot=snapshot,
                    ttl_seconds=self._policy.confirmation_ttl_seconds,
                )
                token = issued.token
                version = issued.version
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - fail closed without a token
                error.__traceback__ = None
                error.__context__ = None
                error.__cause__ = None
        return RouteDecision(
            mode=None,
            needs_user_choice=True,
            status="waiting_user_mode",
            assessments=assessments,
            clarification_reason=reason,
            options=EXECUTABLE_MODES,
            decision_token=token,
            version=version,
            risk=risk,
            requires_approval=requires_approval,
            permissions_still_apply=True,
        )


def _risk_rank(value: RiskLevel) -> int:
    return {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}[value]
