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
    conflict_decision_margin: float = 0.15
    max_single_cost_usd: Decimal = Decimal("0.50")
    max_total_cost_usd: Decimal = Decimal("0.75")
    classifier_timeout_seconds: float = 20
    classifier_cleanup_grace_seconds: float = 0.1
    max_detached_classifier_tasks: int = 32
    confirmation_ttl_seconds: float = 300
    parallel_classifiers: bool = True
    allow_single_classifier_decision: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.confidence_threshold, bool)
            or not isinstance(self.confidence_threshold, int | float)
            or not math.isfinite(self.confidence_threshold)
            or not 0 <= self.confidence_threshold <= 1
        ):
            raise ValueError("confidence threshold must be finite and between zero and one")
        if (
            isinstance(self.conflict_decision_margin, bool)
            or not isinstance(self.conflict_decision_margin, int | float)
            or not math.isfinite(self.conflict_decision_margin)
            or not 0 <= self.conflict_decision_margin <= 1
        ):
            raise ValueError("conflict decision margin must be finite and between zero and one")
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
        if not isinstance(self.parallel_classifiers, bool):
            raise TypeError("parallel_classifiers must be a boolean")
        if not isinstance(self.allow_single_classifier_decision, bool):
            raise TypeError("allow_single_classifier_decision must be a boolean")


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
        self._classifier_task_slots = 0
        self._active_classifier_tasks: set[asyncio.Task[object]] = set()
        self._detached_classifier_tasks: set[asyncio.Task[object]] = set()

    async def route(
        self,
        task_text: object,
        *,
        confirmation_subject: ConfirmationSubject | None = None,
    ) -> RouteDecision:
        cancellation: asyncio.CancelledError | None = None
        try:
            return await self._route_with_text(task_text, confirmation_subject=confirmation_subject)
        except asyncio.CancelledError as error:
            cancellation = _clear_cancellation(error)
        finally:
            task_text = None
            del task_text
        if cancellation is None:  # pragma: no cover - defensive control-flow guard
            raise RuntimeError("routing cancellation boundary failed")
        raise cancellation from None

    async def _route_with_text(
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
        sorted_assessments = tuple(
            sorted(assessments, key=lambda item: item.confidence, reverse=True)
        )
        mode_conflict = any(item.mode is not assessments[0].mode for item in assessments[1:])
        clear_conflict_winner = (
            mode_conflict
            and sorted_assessments[0].confidence >= self._policy.confidence_threshold
            and (
                len(sorted_assessments) == 1
                or sorted_assessments[0].confidence - sorted_assessments[1].confidence
                >= self._policy.conflict_decision_margin
            )
        )
        if (
            (
                any(item.confidence < self._policy.confidence_threshold for item in assessments)
                and not clear_conflict_winner
            )
            or (mode_conflict and not clear_conflict_winner)
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
        if clear_conflict_winner:
            return self._ready(
                sorted_assessments[0].mode,
                (sorted_assessments[0],),
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
        cancellation: asyncio.CancelledError | None = None
        try:
            return await self._classify_with_text(task_text)
        except asyncio.CancelledError as error:
            cancellation = _clear_cancellation(error)
        finally:
            task_text = ""
            del task_text
        if cancellation is None:  # pragma: no cover - defensive control-flow guard
            raise RuntimeError("classifier cancellation boundary failed")
        raise cancellation from None

    async def _classify_with_text(self, task_text: str) -> tuple[RouteAssessment, ...] | None:
        if not self._policy.parallel_classifiers:
            try:
                primary = self._validate_assessment(
                    await self._classifier.classify(task_text),
                    RouteSource.CLASSIFIER,
                )
                if primary is None:
                    return None
                if self._policy.allow_single_classifier_decision:
                    return (primary,)
                verifier = self._validate_assessment(
                    await self._verifier.classify(task_text),
                    RouteSource.VERIFIER,
                )
                if verifier is None:
                    return None
                return (primary, verifier)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                _clear_exception(error)
                return None
        if self._classifier_task_slots + 2 > self._policy.max_detached_classifier_tasks:
            return None
        classifiers = (self._classifier, self._verifier)
        sources = (RouteSource.CLASSIFIER, RouteSource.VERIFIER)
        task_list: list[asyncio.Task[object]] = []
        construction_error: BaseException | None = None
        cancellation: asyncio.CancelledError | None = None
        for index, (classifier, source) in enumerate(zip(classifiers, sources, strict=True)):
            runner = self._classifier_runner(classifier, task_text, source)
            task: asyncio.Task[object] | None = None
            try:
                task = asyncio.create_task(runner, name=f"route-classifier-{index}")
                self._classifier_task_slots += 1
                task_list.append(task)
                self._active_classifier_tasks.add(task)
            except BaseException as error:  # noqa: BLE001 - task construction boundary
                if task is None:
                    runner.close()
                construction_error = _clear_exception(error)
                if isinstance(error, asyncio.CancelledError):
                    cancellation = error
                break
        tasks = tuple(task_list)
        result: tuple[RouteAssessment, ...] | None = None
        try:
            if construction_error is None:
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
                                except asyncio.CancelledError as error:
                                    cancellation = _clear_cancellation(error)
                                    pending = set()
                                    break
                                except Exception as error:  # noqa: BLE001
                                    _clear_exception(error)
                                    pending = set()
                                    break
                except TimeoutError:
                    completed = {}
                if len(completed) == len(tasks):
                    validated = tuple(
                        self._validate_assessment(completed[task], source)
                        for task, source in zip(tasks, sources, strict=True)
                    )
                    if not any(item is None for item in validated):
                        result = tuple(item for item in validated if item is not None)
        except asyncio.CancelledError as error:
            cancellation = _clear_cancellation(error)
        finally:
            try:
                await self._bounded_classifier_cleanup(tasks)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = _clear_cancellation(error)
                else:
                    _clear_cancellation(error)
        if cancellation is not None:
            raise cancellation from None
        if isinstance(construction_error, (KeyboardInterrupt, SystemExit)):
            raise construction_error from None
        return result

    @staticmethod
    async def _classifier_runner(
        classifier: RouteClassifier,
        task_text: str,
        source: RouteSource,
    ) -> object:
        try:
            del source
            return await classifier.classify(task_text)
        finally:
            task_text = ""
            del task_text

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
            self._release_classifier_slot()
            self._consume_classifier_task(task)
        for task in pending:
            task.cancel()
            if self._register_detached_classifier_task(task):
                self._active_classifier_tasks.discard(task)
        if interrupted is not None:
            raise interrupted

    def _register_detached_classifier_task(self, task: asyncio.Task[object]) -> bool:
        if task.done():
            self._release_classifier_slot()
            self._consume_classifier_task(task)
            return True
        try:
            self._detached_classifier_tasks.add(task)
        except BaseException as error:  # noqa: BLE001 - retain active fallback capacity
            _clear_exception(error)
            try:
                task.add_done_callback(self._active_classifier_done)
            except BaseException as callback_error:  # noqa: BLE001 - fail closed in active set
                _clear_exception(callback_error)
            return False
        try:
            task.add_done_callback(self._detached_classifier_done)
        except BaseException as error:  # noqa: BLE001 - detached set remains a bounded fallback
            _clear_exception(error)
        return True

    def _active_classifier_done(self, task: asyncio.Task[object]) -> None:
        self._active_classifier_tasks.discard(task)
        self._release_classifier_slot()
        self._consume_classifier_task(task)

    def _detached_classifier_done(self, task: asyncio.Task[object]) -> None:
        self._detached_classifier_tasks.discard(task)
        self._release_classifier_slot()
        self._consume_classifier_task(task)

    def _release_classifier_slot(self) -> None:
        if self._classifier_task_slots > 0:
            self._classifier_task_slots -= 1

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
        cancellation: asyncio.CancelledError | None = None
        consumed: ConsumedDecisionToken | None = None
        try:
            consumed = await self._consume_confirmation(
                mode,
                plaintext_token=plaintext_token,
                version=version,
                confirmation_subject=confirmation_subject,
            )
        except asyncio.CancelledError as error:
            cancellation = _clear_cancellation(error)
        finally:
            plaintext_token = None
            del plaintext_token
        if cancellation is not None:
            raise cancellation from None
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
        cancellation: asyncio.CancelledError | None = None
        consumed: ConsumedDecisionToken | None = None
        try:
            try:
                confirmation_subject = ConfirmationSubject.model_validate(
                    confirmation_subject.model_dump(round_trip=True), strict=True
                )
            except Exception as error:  # noqa: BLE001 - hostile subject boundary
                _clear_exception(error)
                return None
            if plaintext_token is None or type(version) is not int or version <= 0:
                return None
            if not isinstance(mode, TaskMode) or mode not in EXECUTABLE_MODES:
                raise ValueError("confirmed mode must be executable") from None
            try:
                consumed = await self._token_store.consume(
                    plaintext_token,
                    confirmation_subject,
                    version=version,
                    selected_mode=mode,
                )
            except asyncio.CancelledError as error:
                cancellation = _clear_cancellation(error)
            except Exception as error:  # noqa: BLE001 - stable confirmation boundary
                _clear_exception(error)
        finally:
            plaintext_token = None
            del plaintext_token
        if cancellation is not None:
            raise cancellation from None
        return consumed

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


def _clear_exception(error: BaseException) -> BaseException:
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    return error


def _clear_cancellation(error: asyncio.CancelledError) -> asyncio.CancelledError:
    _clear_exception(error)
    return error
