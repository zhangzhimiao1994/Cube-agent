from __future__ import annotations

from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import _producer_step_timeout
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose


def _context(timeout_seconds: float) -> TaskContext:
    return TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="Build and verify a project artifact.",
        timeout_seconds=timeout_seconds,
        token_budget=1_000_000,
    )


def _role(role_id: str, *, purpose: RolePurpose = RolePurpose.EXECUTE) -> RoleAssignment:
    return RoleAssignment(
        id=role_id,
        role=role_id.title(),
        purpose=purpose,
        mission="Complete the assigned project delivery work.",
        must_answer=("result",),
        allowed_tools=(),
        forbidden_actions=("do not request extra user input",),
        skills=(),
        output_schema={"summary": "string"},
        model="qwen",
    )


def test_producer_step_timeout_keeps_short_interaction_budget_tight() -> None:
    roles = (
        _role("architect"),
        _role("implementer"),
        _role("tester", purpose=RolePurpose.VERIFY),
        _role("security", purpose=RolePurpose.RISK_REVIEW),
    )

    assert _producer_step_timeout(_context(300), roles) == 120


def test_producer_step_timeout_reserves_fallback_room_for_project_scale_runs() -> None:
    roles = (
        _role("architect"),
        _role("implementer"),
        _role("tester", purpose=RolePurpose.VERIFY),
        _role("security", purpose=RolePurpose.RISK_REVIEW),
    )

    assert _producer_step_timeout(_context(900), roles) == 405
