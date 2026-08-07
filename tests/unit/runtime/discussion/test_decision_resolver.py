import pytest

from agent_hub.runtime.discussion.decision import (
    AgentPosition,
    DecisionCriterion,
    DecisionResolver,
    DecisionStatus,
    DisagreementKind,
    ResolutionRequest,
)


def position(
    agent_id: str,
    option_id: str,
    *,
    confidence: float = 0.8,
    goal_fit: float = 0.8,
    safety: float = 0.8,
    verifiability: float = 0.8,
    implementation_cost: float = 0.8,
    maintainability: float = 0.8,
) -> AgentPosition:
    return AgentPosition(
        agent_id=agent_id,
        option_id=option_id,
        summary=f"{agent_id} recommends {option_id}",
        confidence=confidence,
        evidence=(f"{option_id} evidence",),
        assumptions=("bounded assumption",),
        risks=("bounded risk",),
        verification=f"verify {option_id}",
        ratings={
            DecisionCriterion.GOAL_FIT: goal_fit,
            DecisionCriterion.SAFETY: safety,
            DecisionCriterion.VERIFIABILITY: verifiability,
            DecisionCriterion.IMPLEMENTATION_COST: implementation_cost,
            DecisionCriterion.MAINTAINABILITY: maintainability,
        },
    )


def test_strategy_disagreement_selects_highest_weighted_option_with_audit_memo() -> None:
    result = DecisionResolver().resolve(
        ResolutionRequest(
            task="choose a discussion outcome",
            disagreement_kind=DisagreementKind.STRATEGY,
            positions=(
                position(
                    "analyst",
                    "option-a",
                    goal_fit=0.55,
                    safety=0.6,
                    verifiability=0.6,
                    implementation_cost=0.7,
                    maintainability=0.6,
                ),
                position("critic", "option-b", goal_fit=0.95, safety=0.95),
                position("builder", "option-b", goal_fit=0.9, safety=0.9),
            ),
        )
    )

    assert result.status is DecisionStatus.SELECTED
    assert result.selected_option_id == "option-b"
    assert result.requires_user is False
    assert result.scores["option-b"] > result.scores["option-a"]
    assert "Conclusion: select option-b" in result.memo
    assert "Rejected options: option-a" in result.memo


def test_close_strategy_scores_ask_user_instead_of_guessing() -> None:
    result = DecisionResolver().resolve(
        ResolutionRequest(
            task="choose a near tie",
            disagreement_kind=DisagreementKind.STRATEGY,
            positions=(
                position("analyst", "option-a", confidence=0.82, goal_fit=0.8),
                position("critic", "option-b", confidence=0.81, goal_fit=0.79),
            ),
        )
    )

    assert result.status is DecisionStatus.NEEDS_USER
    assert result.selected_option_id is None
    assert result.requires_user is True
    assert result.reason == "top_options_too_close"


@pytest.mark.parametrize(
    ("kind", "high_risk", "reason"),
    [
        (DisagreementKind.AUTHORITY, False, "user_authority_required"),
        (DisagreementKind.STRATEGY, True, "high_risk_decision_requires_user"),
    ],
)
def test_authority_or_high_risk_disagreements_are_escalated_to_user(
    kind: DisagreementKind,
    high_risk: bool,
    reason: str,
) -> None:
    result = DecisionResolver().resolve(
        ResolutionRequest(
            task="decide whether to mutate external state",
            disagreement_kind=kind,
            positions=(
                position("operator", "ship-now"),
                position("reviewer", "wait"),
            ),
            high_risk=high_risk,
        )
    )

    assert result.status is DecisionStatus.NEEDS_USER
    assert result.requires_user is True
    assert result.reason == reason


def test_fact_disagreement_requires_verification_before_decision() -> None:
    result = DecisionResolver().resolve(
        ResolutionRequest(
            task="decide which provider API is current",
            disagreement_kind=DisagreementKind.FACT,
            positions=(
                position("researcher", "api-a"),
                position("reviewer", "api-b"),
            ),
        )
    )

    assert result.status is DecisionStatus.NEEDS_VERIFICATION
    assert result.selected_option_id is None
    assert result.reason == "fact_conflict_requires_verification"


def test_verified_fact_disagreement_selects_verified_option() -> None:
    result = DecisionResolver().resolve(
        ResolutionRequest(
            task="decide which provider API is current",
            disagreement_kind=DisagreementKind.FACT,
            positions=(
                position("researcher", "api-a"),
                position("reviewer", "api-b"),
            ),
            verified_option_id="api-b",
        )
    )

    assert result.status is DecisionStatus.SELECTED
    assert result.selected_option_id == "api-b"
    assert result.reason == "verified_fact"


def test_low_confidence_best_option_asks_user() -> None:
    result = DecisionResolver(min_confidence=0.7).resolve(
        ResolutionRequest(
            task="choose under uncertainty",
            disagreement_kind=DisagreementKind.STRATEGY,
            positions=(
                position("analyst", "option-a", confidence=0.62, goal_fit=0.9),
                position("critic", "option-b", confidence=0.4, goal_fit=0.2),
            ),
        )
    )

    assert result.status is DecisionStatus.NEEDS_USER
    assert result.reason == "low_confidence"
