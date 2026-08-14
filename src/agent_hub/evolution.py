from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvolutionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(skill_distillation|skill_optimization|media_strategy|academic_research|custom)$")
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=10_000)
    mode: str = Field(default="hybrid", pattern=r"^(auto|direct|dispatch|discuss|hybrid)$")
    source_skill_ids: list[str] = Field(default_factory=list, max_length=32)
    source_conversation_id: str | None = Field(default=None, max_length=200)
    source_run_id: str | None = Field(default=None, max_length=200)
    target_artifact_type: str = Field(default="skill", pattern=r"^(skill|strategy|research_gap|paper_plan|media_plan|custom)$")
    max_rounds: int = Field(default=3, ge=1, le=20)
    min_delta: float = Field(default=2.0, ge=0.0, le=100.0)
    budget_tokens: int = Field(default=200_000, ge=1_000, le=50_000_000)
    budget_minutes: int = Field(default=120, ge=1, le=10_080)
    rubric: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("source_skill_ids", "rubric")
    @classmethod
    def validate_bounded_strings(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 256 or item != item.strip():
                raise ValueError("items must be nonblank bounded strings")
        return value


class EvolutionRoundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changed_dimension: str = Field(min_length=1, max_length=200)
    candidate_summary: str = Field(min_length=1, max_length=4_000)
    score_before: float = Field(ge=0.0, le=100.0)
    score_after: float = Field(ge=0.0, le=100.0)
    tests_passed: bool = True
    regression_detected: bool = False
    accepted: bool | None = None
    judge_summary: str = Field(default="", max_length=8_000)
    artifact_refs: list[str] = Field(default_factory=list, max_length=64)
    tokens_used: int = Field(default=0, ge=0, le=50_000_000)
    elapsed_seconds: int = Field(default=0, ge=0, le=604_800)

    @field_validator("artifact_refs")
    @classmethod
    def validate_artifact_refs(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 512 or item != item.strip():
                raise ValueError("artifact refs must be nonblank bounded strings")
        return value


class EvolutionRoundResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=1)
    changed_dimension: str
    candidate_summary: str
    score_before: float
    score_after: float
    delta: float
    tests_passed: bool
    regression_detected: bool
    accepted: bool
    recommendation: str = Field(pattern=r"^(continue|observe_one_more_round|stop|rollback)$")
    stop_reason: str | None = None
    judge_summary: str
    artifact_refs: list[str]
    tokens_used: int
    elapsed_seconds: int
    created_at: datetime


class EvolutionRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^evolution_[a-f0-9]+$")
    kind: str
    title: str
    objective: str
    mode: str
    source_skill_ids: list[str]
    source_conversation_id: str | None
    source_run_id: str | None
    target_artifact_type: str
    status: str = Field(pattern=r"^(waiting_approval|running|stopped|completed)$")
    max_rounds: int
    min_delta: float
    budget_tokens: int
    budget_minutes: int
    rubric: list[str]
    rounds: list[EvolutionRoundResponse] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    updated_at: datetime
    stop_reason: str | None = None


def create_evolution_run_response(request: EvolutionRunRequest, *, actor: str) -> EvolutionRunResponse:
    now = datetime.now(UTC)
    return EvolutionRunResponse(
        id=f"evolution_{uuid4().hex}",
        kind=request.kind,
        title=request.title,
        objective=request.objective,
        mode=request.mode,
        source_skill_ids=request.source_skill_ids,
        source_conversation_id=request.source_conversation_id,
        source_run_id=request.source_run_id,
        target_artifact_type=request.target_artifact_type,
        status="waiting_approval",
        max_rounds=request.max_rounds,
        min_delta=request.min_delta,
        budget_tokens=request.budget_tokens,
        budget_minutes=request.budget_minutes,
        rubric=request.rubric,
        rounds=[],
        created_by=actor,
        created_at=now,
        updated_at=now,
    )


def append_evolution_round(current: EvolutionRunResponse, request: EvolutionRoundRequest) -> EvolutionRunResponse:
    round_response = evolution_round_response(current, request)
    rounds = [*current.rounds, round_response]
    status = evolution_status_after_round(current, rounds, round_response)
    return current.model_copy(
        update={
            "status": status,
            "rounds": rounds,
            "updated_at": datetime.now(UTC),
            "stop_reason": round_response.stop_reason if status in {"stopped", "completed"} else current.stop_reason,
        }
    )


def evolution_round_response(current: EvolutionRunResponse, request: EvolutionRoundRequest) -> EvolutionRoundResponse:
    round_number = len(current.rounds) + 1
    delta = round(request.score_after - request.score_before, 3)
    accepted = request.accepted if request.accepted is not None else delta > 0 and request.tests_passed and not request.regression_detected
    recommendation, stop_reason = evolution_recommendation(current, request, round_number, delta, accepted)
    return EvolutionRoundResponse(
        round=round_number,
        changed_dimension=request.changed_dimension,
        candidate_summary=request.candidate_summary,
        score_before=request.score_before,
        score_after=request.score_after,
        delta=delta,
        tests_passed=request.tests_passed,
        regression_detected=request.regression_detected,
        accepted=accepted,
        recommendation=recommendation,
        stop_reason=stop_reason,
        judge_summary=request.judge_summary,
        artifact_refs=request.artifact_refs,
        tokens_used=request.tokens_used,
        elapsed_seconds=request.elapsed_seconds,
        created_at=datetime.now(UTC),
    )


def evolution_recommendation(
    current: EvolutionRunResponse,
    request: EvolutionRoundRequest,
    round_number: int,
    delta: float,
    accepted: bool,
) -> tuple[str, str | None]:
    if request.regression_detected or not request.tests_passed or not accepted or delta <= 0:
        return "rollback", "tests regressed or score did not improve"
    if round_number >= current.max_rounds:
        return "stop", "maximum rounds reached"
    if delta < current.min_delta:
        previous = current.rounds[-1] if current.rounds else None
        if previous is not None and previous.delta < current.min_delta:
            return "stop", "two consecutive rounds below minimum delta"
        return "observe_one_more_round", "delta is below the minimum improvement threshold"
    return "continue", None


def evolution_status_after_round(
    current: EvolutionRunResponse,
    rounds: list[EvolutionRoundResponse],
    latest: EvolutionRoundResponse,
) -> str:
    if latest.recommendation in {"rollback", "stop"}:
        return "stopped"
    if len(rounds) >= current.max_rounds:
        return "completed"
    return "running"


__all__ = [
    "EvolutionRoundRequest",
    "EvolutionRoundResponse",
    "EvolutionRunRequest",
    "EvolutionRunResponse",
    "append_evolution_round",
    "create_evolution_run_response",
]