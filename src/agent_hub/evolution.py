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
    baseline_agent_id: str | None = Field(default=None, max_length=200)
    candidate_agent_ids: list[str] = Field(default_factory=list, max_length=16)
    evaluator_agent_id: str | None = Field(default=None, max_length=200)
    approval_policy: str = Field(default="ask", pattern=r"^(ask|auto|manual)$")
    iteration_policy: str = Field(default="score_gated", pattern=r"^(score_gated|fixed_rounds|manual_review)$")
    memory_policy: str = Field(default="summarize_between_rounds", pattern=r"^(none|summarize_between_rounds|full_ledger)$")
    max_rounds: int = Field(default=3, ge=1, le=20)
    min_delta: float = Field(default=2.0, ge=0.0, le=100.0)
    budget_tokens: int = Field(default=200_000, ge=1_000, le=50_000_000)
    budget_minutes: int = Field(default=120, ge=1, le=10_080)
    rubric: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("source_skill_ids", "candidate_agent_ids", "rubric")
    @classmethod
    def validate_bounded_strings(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 256 or item != item.strip():
                raise ValueError("items must be nonblank bounded strings")
        return value


class EvolutionApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = True
    baseline_agent_id: str | None = Field(default=None, max_length=200)
    evaluator_agent_id: str | None = Field(default=None, max_length=200)
    note: str = Field(default="", max_length=2_000)


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


class EvolutionNextRoundPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    round: int = Field(ge=1)
    action: str = Field(pattern=r"^(run_next_round|review_baseline|rollback_candidate)$")
    task_title: str
    task_prompt: str
    baseline_agent_id: str
    candidate_agent_ids: list[str]
    evaluator_agent_id: str
    memory_policy: str
    required_output_schema: dict[str, str]
    previous_rounds: list[str]


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
    source_conversation_id: str | None = None
    source_run_id: str | None = None
    target_artifact_type: str
    baseline_agent_id: str | None = None
    candidate_agent_ids: list[str] = Field(default_factory=list)
    evaluator_agent_id: str | None = None
    approval_policy: str = "ask"
    approval_status: str = Field(default="pending", pattern=r"^(pending|approved|rejected)$")
    approved_by: str | None = None
    approved_at: datetime | None = None
    approval_note: str = ""
    iteration_policy: str = "score_gated"
    memory_policy: str = "summarize_between_rounds"
    next_action: str = Field(
        default="request_approval",
        pattern=r"^(request_approval|run_next_round|review_baseline|rollback_candidate|stop|completed)$",
    )
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
    auto_approved = request.approval_policy == "auto"
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
        baseline_agent_id=request.baseline_agent_id,
        candidate_agent_ids=request.candidate_agent_ids,
        evaluator_agent_id=request.evaluator_agent_id,
        approval_policy=request.approval_policy,
        approval_status="approved" if auto_approved else "pending",
        approved_by=actor if auto_approved else None,
        approved_at=now if auto_approved else None,
        approval_note="auto approved by policy" if auto_approved else "",
        iteration_policy=request.iteration_policy,
        memory_policy=request.memory_policy,
        next_action="run_next_round" if auto_approved else "request_approval",
        status="running" if auto_approved else "waiting_approval",
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


def approve_evolution_run_response(
    current: EvolutionRunResponse,
    request: EvolutionApprovalRequest,
    *,
    actor: str,
) -> EvolutionRunResponse:
    now = datetime.now(UTC)
    if current.status in {"stopped", "completed"}:
        return current.model_copy(update={"updated_at": now})
    if not request.approved:
        return current.model_copy(
            update={
                "status": "stopped",
                "approval_status": "rejected",
                "approved_by": actor,
                "approved_at": now,
                "approval_note": request.note,
                "next_action": "stop",
                "stop_reason": request.note or "approval rejected",
                "updated_at": now,
            }
        )
    return current.model_copy(
        update={
            "status": "running",
            "approval_status": "approved",
            "approved_by": actor,
            "approved_at": now,
            "approval_note": request.note,
            "baseline_agent_id": request.baseline_agent_id or current.baseline_agent_id,
            "evaluator_agent_id": request.evaluator_agent_id or current.evaluator_agent_id,
            "next_action": "run_next_round",
            "updated_at": now,
        }
    )


def append_evolution_round(current: EvolutionRunResponse, request: EvolutionRoundRequest) -> EvolutionRunResponse:
    round_response = evolution_round_response(current, request)
    rounds = [*current.rounds, round_response]
    status = evolution_status_after_round(current, rounds, round_response)
    next_action = evolution_next_action_after_round(status, round_response)
    return current.model_copy(
        update={
            "status": status,
            "rounds": rounds,
            "next_action": next_action,
            "updated_at": datetime.now(UTC),
            "stop_reason": round_response.stop_reason if status in {"stopped", "completed"} else current.stop_reason,
        }
    )


def plan_evolution_next_round(current: EvolutionRunResponse) -> EvolutionNextRoundPlanResponse:
    round_number = len(current.rounds) + 1
    action = current.next_action
    if action not in {"run_next_round", "review_baseline", "rollback_candidate"}:
        action = "run_next_round"
    baseline_agent_id = current.baseline_agent_id or "main-agent"
    candidate_agent_ids = current.candidate_agent_ids or ["candidate-agent"]
    evaluator_agent_id = current.evaluator_agent_id or baseline_agent_id
    previous_rounds = [
        (
            f"Round {item.round}: {item.changed_dimension}; "
            f"delta={item.delta}; accepted={item.accepted}; "
            f"recommendation={item.recommendation}"
        )
        for item in current.rounds[-5:]
    ]
    task_prompt = _evolution_next_round_prompt(
        current,
        round_number=round_number,
        action=action,
        baseline_agent_id=baseline_agent_id,
        candidate_agent_ids=candidate_agent_ids,
        evaluator_agent_id=evaluator_agent_id,
        previous_rounds=previous_rounds,
    )
    return EvolutionNextRoundPlanResponse(
        run_id=current.id,
        round=round_number,
        action=action,
        task_title=f"{current.title} / round {round_number}",
        task_prompt=task_prompt,
        baseline_agent_id=baseline_agent_id,
        candidate_agent_ids=candidate_agent_ids,
        evaluator_agent_id=evaluator_agent_id,
        memory_policy=current.memory_policy,
        required_output_schema={
            "changed_dimension": "The tested dimension changed in this round.",
            "candidate_summary": "Concrete candidate change summary.",
            "score_before": "Baseline score before applying the candidate.",
            "score_after": "Candidate score after evaluation.",
            "tests_passed": "Whether required tests passed.",
            "regression_detected": "Whether any regression was detected.",
            "judge_summary": "Evaluator evidence and reasoning summary.",
            "artifact_refs": "Generated artifact or report references.",
            "tokens_used": "Total tokens consumed by this round.",
            "elapsed_seconds": "Elapsed execution time in seconds.",
        },
        previous_rounds=previous_rounds,
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


def _evolution_next_round_prompt(
    current: EvolutionRunResponse,
    *,
    round_number: int,
    action: str,
    baseline_agent_id: str,
    candidate_agent_ids: list[str],
    evaluator_agent_id: str,
    previous_rounds: list[str],
) -> str:
    source_skills = ", ".join(current.source_skill_ids) or "none"
    candidates = ", ".join(candidate_agent_ids)
    rubric = "; ".join(current.rubric) or "use the objective-specific rubric"
    previous = "\n".join(previous_rounds) if previous_rounds else "No previous rounds."
    return (
        f"Evolution run: {current.title}\n"
        f"Objective: {current.objective}\n"
        f"Round: {round_number}\n"
        f"Action: {action}\n"
        f"Target artifact type: {current.target_artifact_type}\n"
        f"Source skills: {source_skills}\n"
        f"Baseline agent: {baseline_agent_id}\n"
        f"Candidate agents: {candidates}\n"
        f"Evaluator agent: {evaluator_agent_id}\n"
        f"Iteration policy: {current.iteration_policy}; min_delta={current.min_delta}; "
        f"max_rounds={current.max_rounds}\n"
        f"Memory policy: {current.memory_policy}\n"
        f"Rubric: {rubric}\n"
        f"Previous rounds:\n{previous}\n"
        "Execute one bounded evolution round. Compare the candidate against the baseline "
        "with the fixed evaluation set, reject regressions, and return an EvolutionRoundRequest "
        "JSON object containing changed_dimension, candidate_summary, score_before, "
        "score_after, tests_passed, regression_detected, judge_summary, artifact_refs, "
        "tokens_used, and elapsed_seconds. Do not publish or install the candidate directly."
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


def evolution_next_action_after_round(status: str, latest: EvolutionRoundResponse) -> str:
    if status == "completed":
        return "completed"
    if latest.recommendation == "rollback":
        return "rollback_candidate"
    if latest.recommendation == "stop":
        return "stop"
    if latest.recommendation == "observe_one_more_round":
        return "review_baseline"
    return "run_next_round"


__all__ = [
    "EvolutionApprovalRequest",
    "EvolutionNextRoundPlanResponse",
    "EvolutionRoundRequest",
    "EvolutionRoundResponse",
    "EvolutionRunRequest",
    "EvolutionRunResponse",
    "append_evolution_round",
    "approve_evolution_run_response",
    "create_evolution_run_response",
    "plan_evolution_next_round",
]
