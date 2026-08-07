"""Framework-neutral disagreement resolution for discussion-mode agents."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class DisagreementKind(StrEnum):
    """Type of conflict produced by a multi-agent discussion."""

    FACT = "fact"
    STRATEGY = "strategy"
    AUTHORITY = "authority"
    UNKNOWN = "unknown"


class DecisionCriterion(StrEnum):
    """Stable scoring dimensions for strategy disagreements."""

    GOAL_FIT = "goal_fit"
    SAFETY = "safety"
    VERIFIABILITY = "verifiability"
    IMPLEMENTATION_COST = "implementation_cost"
    MAINTAINABILITY = "maintainability"


class DecisionStatus(StrEnum):
    """Resolver output state."""

    SELECTED = "selected"
    NEEDS_VERIFICATION = "needs_verification"
    NEEDS_USER = "needs_user"


_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_MAX_TEXT = 2_000
_DEFAULT_WEIGHTS = MappingProxyType({
    DecisionCriterion.GOAL_FIT: 0.30,
    DecisionCriterion.SAFETY: 0.25,
    DecisionCriterion.VERIFIABILITY: 0.20,
    DecisionCriterion.IMPLEMENTATION_COST: 0.15,
    DecisionCriterion.MAINTAINABILITY: 0.10,
})


@dataclass(frozen=True, slots=True)
class AgentPosition:
    """One participant's structured position."""

    agent_id: str
    option_id: str
    summary: str
    confidence: float
    evidence: tuple[str, ...]
    assumptions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    verification: str = ""
    ratings: Mapping[DecisionCriterion, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier("agent_id", self.agent_id)
        _require_identifier("option_id", self.option_id)
        _require_bounded_text("summary", self.summary)
        _normalize_unit_float("confidence", self.confidence)
        evidence = _normalize_text_tuple("evidence", self.evidence, min_length=1)
        assumptions = _normalize_text_tuple("assumptions", self.assumptions)
        risks = _normalize_text_tuple("risks", self.risks)
        if self.verification:
            _require_bounded_text("verification", self.verification)
        ratings = _normalize_ratings(self.ratings)

        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "assumptions", assumptions)
        object.__setattr__(self, "risks", risks)
        object.__setattr__(self, "ratings", ratings)


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    """Input to the decision resolver after a discussion round."""

    task: str
    disagreement_kind: DisagreementKind
    positions: tuple[AgentPosition, ...]
    high_risk: bool = False
    verified_option_id: str | None = None

    def __post_init__(self) -> None:
        _require_bounded_text("task", self.task)
        if type(self.disagreement_kind) is not DisagreementKind:
            raise ValueError("disagreement_kind is invalid")
        if type(self.high_risk) is not bool:
            raise ValueError("high_risk must be a boolean")
        positions = tuple(self.positions)
        if len(positions) < 2:
            raise ValueError("at least two positions are required")
        if not all(isinstance(position, AgentPosition) for position in positions):
            raise ValueError("positions must contain only AgentPosition values")
        agent_ids = [position.agent_id for position in positions]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("duplicate agent position")
        if self.verified_option_id is not None:
            _require_identifier("verified_option_id", self.verified_option_id)
            if self.verified_option_id not in {position.option_id for position in positions}:
                raise ValueError("verified_option_id must match a proposed option")
        object.__setattr__(self, "positions", positions)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Auditable outcome of a disagreement resolution."""

    status: DecisionStatus
    selected_option_id: str | None
    requires_user: bool
    reason: str
    scores: Mapping[str, float]
    memo: str

    def __post_init__(self) -> None:
        if type(self.status) is not DecisionStatus:
            raise ValueError("status is invalid")
        if self.selected_option_id is not None:
            _require_identifier("selected_option_id", self.selected_option_id)
        if type(self.requires_user) is not bool:
            raise ValueError("requires_user must be a boolean")
        _require_identifier("reason", self.reason)
        _require_bounded_text("memo", self.memo, max_length=10_000, allow_newlines=True)
        scores = MappingProxyType({
            option_id: _normalize_unit_float("score", score)
            for option_id, score in self.scores.items()
        })
        object.__setattr__(self, "scores", scores)


class DecisionResolver:
    """Resolve conflicting discussion outcomes without silent mode mistakes."""

    __slots__ = ("_close_margin", "_min_confidence", "_weights")

    def __init__(
        self,
        *,
        close_margin: float = 0.10,
        min_confidence: float = 0.70,
        weights: Mapping[DecisionCriterion, float] = _DEFAULT_WEIGHTS,
    ) -> None:
        self._close_margin = _normalize_unit_float("close_margin", close_margin)
        self._min_confidence = _normalize_unit_float("min_confidence", min_confidence)
        self._weights = _normalize_weights(weights)

    def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        if type(request) is not ResolutionRequest:
            raise ValueError("request must be ResolutionRequest")
        scores = _score_positions(request.positions, self._weights)
        if request.disagreement_kind is DisagreementKind.AUTHORITY:
            return self._needs_user(request, scores, "user_authority_required")
        if request.high_risk:
            return self._needs_user(request, scores, "high_risk_decision_requires_user")
        if request.disagreement_kind is DisagreementKind.FACT:
            if request.verified_option_id is None:
                return ResolutionResult(
                    status=DecisionStatus.NEEDS_VERIFICATION,
                    selected_option_id=None,
                    requires_user=False,
                    reason="fact_conflict_requires_verification",
                    scores=scores,
                    memo=_memo(
                        request,
                        scores,
                        selected_option_id=None,
                        reason="fact_conflict_requires_verification",
                    ),
                )
            return self._selected(request, scores, request.verified_option_id, "verified_fact")

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        selected_option_id, selected_score = ranked[0]
        confidence = _option_confidence(request.positions, selected_option_id)
        if confidence < self._min_confidence:
            return self._needs_user(request, scores, "low_confidence")
        if len(ranked) > 1 and selected_score - ranked[1][1] < self._close_margin:
            return self._needs_user(request, scores, "top_options_too_close")
        return self._selected(request, scores, selected_option_id, "weighted_score")

    def _needs_user(
        self,
        request: ResolutionRequest,
        scores: Mapping[str, float],
        reason: str,
    ) -> ResolutionResult:
        return ResolutionResult(
            status=DecisionStatus.NEEDS_USER,
            selected_option_id=None,
            requires_user=True,
            reason=reason,
            scores=scores,
            memo=_memo(request, scores, selected_option_id=None, reason=reason),
        )

    def _selected(
        self,
        request: ResolutionRequest,
        scores: Mapping[str, float],
        selected_option_id: str,
        reason: str,
    ) -> ResolutionResult:
        return ResolutionResult(
            status=DecisionStatus.SELECTED,
            selected_option_id=selected_option_id,
            requires_user=False,
            reason=reason,
            scores=scores,
            memo=_memo(
                request,
                scores,
                selected_option_id=selected_option_id,
                reason=reason,
            ),
        )


def _score_positions(
    positions: tuple[AgentPosition, ...],
    weights: Mapping[DecisionCriterion, float],
) -> Mapping[str, float]:
    weighted_scores: dict[str, float] = {}
    confidence_totals: dict[str, float] = {}
    for position in positions:
        position_score = sum(
            position.ratings[criterion] * weight for criterion, weight in weights.items()
        )
        confidence = max(position.confidence, 0.01)
        weighted_scores[position.option_id] = weighted_scores.get(position.option_id, 0) + (
            position_score * confidence
        )
        confidence_totals[position.option_id] = (
            confidence_totals.get(position.option_id, 0) + confidence
        )
    return MappingProxyType({
        option_id: round(weighted_scores[option_id] / confidence_totals[option_id], 6)
        for option_id in weighted_scores
    })


def _option_confidence(positions: tuple[AgentPosition, ...], option_id: str) -> float:
    confidences = [
        float(position.confidence) for position in positions if position.option_id == option_id
    ]
    return sum(confidences) / len(confidences)


def _memo(
    request: ResolutionRequest,
    scores: Mapping[str, float],
    *,
    selected_option_id: str | None,
    reason: str,
) -> str:
    ranked_options = tuple(option for option, _ in sorted(scores.items(), key=lambda item: -item[1]))
    if selected_option_id is None:
        conclusion = f"Conclusion: ask for {reason}"
        rejected = "Rejected options: none"
    else:
        conclusion = f"Conclusion: select {selected_option_id}"
        rejected_options = [option for option in ranked_options if option != selected_option_id]
        rejected = (
            "Rejected options: " + ", ".join(rejected_options)
            if rejected_options
            else "Rejected options: none"
        )
    score_line = ", ".join(f"{option}={score:.3f}" for option, score in sorted(scores.items()))
    positions = "; ".join(
        f"{position.agent_id}->{position.option_id}({position.confidence:.2f})"
        for position in request.positions
    )
    return (
        f"{conclusion}\n"
        f"Reason: {reason}\n"
        f"Task: {request.task}\n"
        f"Scores: {score_line}\n"
        f"Positions: {positions}\n"
        f"{rejected}"
    )


def _normalize_weights(weights: Mapping[DecisionCriterion, float]) -> Mapping[DecisionCriterion, float]:
    if set(weights) != set(DecisionCriterion):
        raise ValueError("weights must cover every decision criterion exactly once")
    normalized = {
        criterion: _normalize_unit_float(f"weight {criterion.value}", value)
        for criterion, value in weights.items()
    }
    total = sum(normalized.values())
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=0.000001):
        raise ValueError("weights must sum to 1")
    return MappingProxyType(normalized)


def _normalize_ratings(ratings: Mapping[DecisionCriterion, float]) -> Mapping[DecisionCriterion, float]:
    if set(ratings) != set(DecisionCriterion):
        raise ValueError("ratings must cover every decision criterion exactly once")
    return MappingProxyType({
        DecisionCriterion(criterion): _normalize_unit_float(
            f"rating {DecisionCriterion(criterion).value}",
            value,
        )
        for criterion, value in ratings.items()
    })


def _normalize_unit_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    normalized = float(value)
    if not 0 <= normalized <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return normalized


def _require_identifier(name: str, value: str) -> None:
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")


def _require_bounded_text(
    name: str,
    value: str,
    *,
    max_length: int = _MAX_TEXT,
    allow_newlines: bool = False,
) -> None:
    if type(value) is not str or not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{name} must be nonblank, unpadded, and bounded")
    allowed_controls = {"\n"} if allow_newlines else set()
    if any(ord(character) < 32 and character not in allowed_controls for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _normalize_text_tuple(
    name: str,
    values: tuple[str, ...],
    *,
    min_length: int = 0,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if len(normalized) < min_length:
        raise ValueError(f"{name} has too few entries")
    for value in normalized:
        _require_bounded_text(name, value)
    return normalized


__all__ = [
    "AgentPosition",
    "DecisionCriterion",
    "DecisionResolver",
    "DecisionStatus",
    "DisagreementKind",
    "ResolutionRequest",
    "ResolutionResult",
]
